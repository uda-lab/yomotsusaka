"""Drift-detection test for ``docs/error-taxonomy.md``.

Asserts that every value of the five ``*FailureReason`` / ``*Reason`` /
``OperationalCategory`` surfaces appears in ``docs/error-taxonomy.md`` and
that each surface's class name appears as an H2 header (``## <ClassName>``).
When a new enum value is added at the source without being documented here,
this test fails with a message pointing the maintainer at child issue #74
(MVP-4 stacked-PR series) — or, for the operational taxonomy, child issue
#93 (MVP-5) — for guidance.

Four of the five surfaces are ``enum.Enum`` subclasses;
``InferenceBackendReason`` is a ``typing.Literal`` alias. Both shapes are
handled below.
"""

from __future__ import annotations

import re
from enum import Enum
from pathlib import Path
from typing import get_args

from yomotsusaka.boundary import (
    ResolverFailureReason,
    RestorationFailureReason,
)
from yomotsusaka.execution_gateway import ExecutionFailureReason
from yomotsusaka.inference_backend import InferenceBackendReason
from yomotsusaka.operational_taxonomy import OperationalCategory

DOC_PATH = Path(__file__).resolve().parent.parent / "docs" / "error-taxonomy.md"

# Each entry: (display name used in H2 header, iterable of wire-string values).
_ENUM_SURFACES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "ResolverFailureReason",
        tuple(member.value for member in ResolverFailureReason),
    ),
    (
        "RestorationFailureReason",
        tuple(member.value for member in RestorationFailureReason),
    ),
    (
        "ExecutionFailureReason",
        tuple(member.value for member in ExecutionFailureReason),
    ),
    (
        "InferenceBackendReason",
        # ``InferenceBackendReason`` is ``typing.Literal[...]``, not an Enum.
        # ``typing.get_args`` returns the literal string members.
        tuple(get_args(InferenceBackendReason)),
    ),
    (
        "OperationalCategory",
        tuple(member.value for member in OperationalCategory),
    ),
)


def _doc_text() -> str:
    assert DOC_PATH.is_file(), (
        f"expected docs/error-taxonomy.md at {DOC_PATH}; see child issue #74 "
        "(mvp4 error-taxonomy stacked PR) for the doc spec."
    )
    return DOC_PATH.read_text(encoding="utf-8")


def test_doc_exists() -> None:
    """The taxonomy doc file is present."""
    assert DOC_PATH.is_file(), (
        f"missing docs/error-taxonomy.md at {DOC_PATH}; recreate per child #74."
    )


def test_every_enum_class_has_h2_header() -> None:
    """Each enum/literal class name appears as an H2 header in the doc.

    The match is anchored to the start of a line so embedded mentions
    (``:class:`ExecutionFailureReason``` inside prose) do not pass the gate
    by accident.
    """
    text = _doc_text()
    missing: list[str] = []
    for class_name, _values in _ENUM_SURFACES:
        pattern = rf"(?m)^##\s+{re.escape(class_name)}\s*$"
        if re.search(pattern, text) is None:
            missing.append(class_name)
    assert not missing, (
        "docs/error-taxonomy.md is missing H2 headers for: "
        f"{missing}. Add a section per enum class (see child issue #74 "
        "for the required doc shape)."
    )


def test_every_enum_value_appears_in_doc() -> None:
    """Each wire-string value appears at least once in the doc."""
    text = _doc_text()
    missing: list[tuple[str, str]] = []
    for class_name, values in _ENUM_SURFACES:
        for value in values:
            # Wire identifiers are unique strings; substring match is
            # sufficient. The doc renders them inside backticks
            # (e.g. ``policy_denied``) so accidental natural-language
            # overlap is unlikely.
            if value not in text:
                missing.append((class_name, value))
    assert not missing, (
        "docs/error-taxonomy.md is missing the following enum values: "
        f"{missing}. Add a table row per value (see child issue #74 for the "
        "required cell shape: Reason / Surface / Trigger / Owner action)."
    )


def test_surfaces_enumerate_known_enum_shapes() -> None:
    """Defensive: ``_ENUM_SURFACES`` covers every imported enum.

    Guards against a future refactor that adds a new ``*FailureReason`` to
    the imports above without extending the surface tuple. Without this
    guard, a new enum could land with zero documentation pressure.
    """
    expected_names = {
        "ResolverFailureReason",
        "RestorationFailureReason",
        "ExecutionFailureReason",
        "InferenceBackendReason",
        "OperationalCategory",
    }
    surface_names = {name for name, _values in _ENUM_SURFACES}
    assert surface_names == expected_names, (
        f"surface tuple {surface_names} drifted from expected {expected_names}; "
        "update _ENUM_SURFACES to match the five documented enums."
    )


def test_enum_classes_are_non_empty() -> None:
    """Sanity: each surface has at least one value (catches an empty enum)."""
    for class_name, values in _ENUM_SURFACES:
        assert values, f"surface {class_name} has zero values; refusing to vacuously pass"


def test_enum_subclasses_are_str_enums_where_expected() -> None:
    """The three ``Enum`` surfaces are ``str``-valued ``Enum`` subclasses.

    ``InferenceBackendReason`` is intentionally a ``typing.Literal`` alias
    and is exempt from this check.
    """
    for klass in (
        ResolverFailureReason,
        RestorationFailureReason,
        ExecutionFailureReason,
        OperationalCategory,
    ):
        assert issubclass(klass, Enum), f"{klass.__name__} is not an Enum subclass"
        for member in klass:
            assert isinstance(member.value, str), (
                f"{klass.__name__}.{member.name} value is not a string"
            )
