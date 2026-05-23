"""
Restoration policy table (issue #44).

This module declares the **restoration policy table** consulted by
:func:`yomotsusaka.boundary.restoration_request` immediately after the
scope gate and before the intent audit record is written.

The policy table is *not* a substitute for the scope gate. The scope gate
remains the load-bearing fail-closed guard documented in
``docs/architecture.md`` §5.7.2; the policy table is an additional
declarative layer that consumes the four reserved-but-unenforced fields
from MVP-2 (``policy_profile``, ``production_scope``,
``authorization_decision``, ``approval_ticket``) and turns them into
permit / deny verdicts keyed on a profile row.

Design notes
------------

* Load is one-shot. :meth:`RestorationPolicyTable.load_from_path` parses
  the YAML file once and returns a frozen table. No hot-reload, no
  background watcher, no implicit cache: the caller (test fixture or
  facade) owns the lifetime.
* The boundary's *default* table is :meth:`RestorationPolicyTable.default_local`
  — a single permissive row that preserves the MVP-2 contract for callers
  that do not pass an explicit table. Every existing
  ``tests/test_restoration_request.py`` assertion passes unmodified
  against this default.
* Profile selection is deterministic. ``policy_profile=None`` selects the
  row marked ``default: true`` (exactly one). An unknown profile name is a
  deny, not a silent fallback.
* Approval-ticket matching uses :func:`re.fullmatch` on a length-capped
  string (256 chars) to keep a pathological pattern from becoming a DoS
  vector. Patterns that fail :func:`re.compile` at load time raise
  ``ValueError``.

See ``docs/architecture.md`` §5.7.3 for the architectural placement of
the policy table relative to the resolver and the restoration request
flow.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# Bound applied to ``approval_ticket`` before regex matching. Mirrors the
# 256-char cap discussed in the lite-spec under "Regex DoS".
_APPROVAL_TICKET_MAX_LEN = 256

# Sentinel for "any production_scope is permitted by this row".
_PRODUCTION_SCOPE_WILDCARD = "*"


class RestorationPolicyRow(BaseModel, frozen=True):
    """One row of the restoration policy table.

    The constructor compiles ``approval_ticket_pattern`` eagerly so that a
    malformed regex fails at table-load time rather than on first match.
    """

    model_config = ConfigDict(extra="forbid")

    profile_name: str = Field(min_length=1)
    production_scopes: list[str] = Field(default_factory=list)
    require_authorization_decision: bool = False
    approval_ticket_pattern: str | None = None
    default: bool = False

    @field_validator("profile_name")
    @classmethod
    def _profile_name_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("profile_name must be non-empty after strip")
        return v

    @field_validator("production_scopes")
    @classmethod
    def _production_scopes_well_formed(cls, v: list[str]) -> list[str]:
        # An empty production_scopes list means "no production_scope value
        # is permitted by this row". That is a valid, restrictive
        # configuration (the row will only match requests whose
        # production_scope is None — see ``evaluate``). What we *don't*
        # allow is whitespace-only or non-string entries.
        for item in v:
            if not isinstance(item, str) or not item.strip():
                raise ValueError(
                    "production_scopes entries must be non-empty strings"
                )
        return v

    @field_validator("approval_ticket_pattern")
    @classmethod
    def _approval_ticket_pattern_compiles(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not isinstance(v, str) or not v:
            raise ValueError("approval_ticket_pattern must be a non-empty string or null")
        try:
            re.compile(v)
        except re.error as exc:
            # Raise a clean ValueError so the loader can surface a YAML
            # row identifier without echoing the underlying regex
            # diagnostic verbatim (the message itself is safe to surface
            # — it does not contain private values — but we still wrap
            # it).
            raise ValueError(f"approval_ticket_pattern does not compile: {exc}") from exc
        return v

    def permits_production_scope(self, value: str | None) -> bool:
        """Return ``True`` iff this row permits *value* as ``production_scope``."""
        if _PRODUCTION_SCOPE_WILDCARD in self.production_scopes:
            return True
        if value is None:
            # A row with no explicit production_scopes accepts the "no
            # production_scope supplied" case; otherwise it must be in
            # the list.
            return not self.production_scopes
        return value in self.production_scopes


class PolicyDecision(BaseModel, frozen=True):
    """Verdict returned by :meth:`RestorationPolicyTable.evaluate`."""

    model_config = ConfigDict(extra="forbid")

    verdict: Literal["permit", "deny"]
    matched_profile: str
    deny_reason: str | None = None

    @model_validator(mode="after")
    def _check_outcome_invariants(self) -> "PolicyDecision":
        if self.verdict == "permit" and self.deny_reason is not None:
            raise ValueError("permit verdict must not carry a deny_reason")
        if self.verdict == "deny" and not self.deny_reason:
            raise ValueError("deny verdict must carry a non-empty deny_reason")
        return self


class RestorationPolicyTable:
    """Frozen collection of :class:`RestorationPolicyRow` keyed by ``profile_name``.

    The constructor validates the table's global invariants (exactly one
    default row, unique profile names) so the loader can fail loud on a
    misconfigured YAML file. ``evaluate`` is pure and does no I/O.
    """

    __slots__ = ("_rows_by_name", "_default_name", "_route_unknown_to_default")

    def __init__(
        self,
        rows: list[RestorationPolicyRow],
        *,
        route_unknown_profile_to_default: bool = False,
    ) -> None:
        """Construct a policy table from *rows*.

        Parameters
        ----------
        rows:
            Profile rows; at least one must mark ``default: true``; exactly
            one (no more, no less) — duplicate ``profile_name`` rows are
            rejected.
        route_unknown_profile_to_default:
            When ``True`` (used only by :meth:`default_local`), an
            unrecognised ``policy_profile`` value silently routes to the
            default row instead of producing a deny verdict. This is
            reserved for the built-in permissive table whose explicit job
            is to preserve the MVP-2 contract for callers that never
            authored an explicit table; user-loaded tables MUST leave it
            ``False`` so an unknown profile name is loud.
        """
        if not rows:
            raise ValueError("RestorationPolicyTable requires at least one row")

        by_name: dict[str, RestorationPolicyRow] = {}
        for row in rows:
            if row.profile_name in by_name:
                raise ValueError(
                    f"duplicate profile_name in policy table: {row.profile_name!r}"
                )
            by_name[row.profile_name] = row

        defaults = [r for r in rows if r.default]
        if len(defaults) == 0:
            raise ValueError(
                "policy table must mark exactly one row as default: true; got 0"
            )
        if len(defaults) > 1:
            raise ValueError(
                "policy table must mark exactly one row as default: true; got "
                f"{len(defaults)} ({[r.profile_name for r in defaults]!r})"
            )

        self._rows_by_name = by_name
        self._default_name = defaults[0].profile_name
        self._route_unknown_to_default = bool(route_unknown_profile_to_default)

    # ------------------------------------------------------------------
    # Constructors
    # ------------------------------------------------------------------

    @classmethod
    def default_local(cls) -> "RestorationPolicyTable":
        """Return the permissive built-in table used when no path is supplied.

        The default row permits any request: any ``production_scope``
        (including ``None``), no required ``authorization_decision``, no
        required ``approval_ticket`` pattern. This preserves the MVP-2
        ``restoration_request`` contract so existing callers and tests
        that do not pass an explicit ``policy_table`` keep working.
        """
        return cls(
            [
                RestorationPolicyRow(
                    profile_name="_default_local",
                    production_scopes=[_PRODUCTION_SCOPE_WILDCARD],
                    require_authorization_decision=False,
                    approval_ticket_pattern=None,
                    default=True,
                )
            ],
            route_unknown_profile_to_default=True,
        )

    @classmethod
    def load_from_path(cls, path: Path) -> "RestorationPolicyTable":
        """Parse the ``restoration:`` section of a YAML policy file.

        The file is expected to carry a top-level ``restoration:`` mapping
        with a ``profiles:`` list whose items match
        :class:`RestorationPolicyRow`. Other top-level keys (notably
        ``redaction:`` documented separately) are ignored — the loader
        cares only about ``restoration:`` so it does not break existing
        readers of the same file.

        Raises ``ValueError`` on any malformed input. Raises ``OSError``
        on read failure (propagated from :meth:`pathlib.Path.read_text`).
        """
        if not isinstance(path, Path):
            raise TypeError(f"path must be a pathlib.Path; got {type(path).__name__}")
        text = path.read_text(encoding="utf-8")
        loaded = yaml.safe_load(text)
        if loaded is None:
            raise ValueError(f"policy YAML at {path.name!r} is empty")
        if not isinstance(loaded, dict):
            raise ValueError(
                f"policy YAML at {path.name!r} must be a mapping at the top level"
            )
        section = loaded.get("restoration")
        if section is None:
            raise ValueError(
                f"policy YAML at {path.name!r} is missing top-level key 'restoration'"
            )
        if not isinstance(section, dict):
            raise ValueError(
                f"policy YAML at {path.name!r}: 'restoration' must be a mapping"
            )
        raw_profiles = section.get("profiles")
        if not isinstance(raw_profiles, list) or not raw_profiles:
            raise ValueError(
                f"policy YAML at {path.name!r}: 'restoration.profiles' must be a "
                "non-empty list"
            )
        rows: list[RestorationPolicyRow] = []
        for idx, raw_row in enumerate(raw_profiles):
            if not isinstance(raw_row, dict):
                raise ValueError(
                    f"policy YAML at {path.name!r}: profiles[{idx}] must be a mapping"
                )
            try:
                rows.append(RestorationPolicyRow.model_validate(raw_row))
            except Exception as exc:  # pydantic ValidationError or our ValueError
                raise ValueError(
                    f"policy YAML at {path.name!r}: profiles[{idx}] is invalid: {exc}"
                ) from exc
        return cls(rows)

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------

    @property
    def default_profile_name(self) -> str:
        """Name of the row marked ``default: true`` (always present)."""
        return self._default_name

    def profile_names(self) -> tuple[str, ...]:
        """Return the profile names in insertion order, as a stable tuple."""
        return tuple(self._rows_by_name.keys())

    def evaluate(
        self,
        *,
        policy_profile: str | None,
        production_scope: str | None,
        authorization_decision: str | None,
        approval_ticket: str | None,
    ) -> PolicyDecision:
        """Return a :class:`PolicyDecision` for the given reserved-field tuple.

        Selection rules:

        * ``policy_profile is None`` → use the row marked ``default: true``.
        * ``policy_profile`` set but not present in the table → deny with
          ``deny_reason`` naming the unknown profile.

        Once a row is selected, the row's three requirements are checked
        in fixed order: ``production_scope`` membership, then
        ``require_authorization_decision``, then
        ``approval_ticket_pattern``. The first failing requirement is the
        reported deny reason. None of these checks ever touches private
        values; they read only the four reserved fields.
        """
        if policy_profile is None:
            row = self._rows_by_name[self._default_name]
            matched = self._default_name
        else:
            row = self._rows_by_name.get(policy_profile)
            if row is None:
                if self._route_unknown_to_default:
                    # Permissive built-in path only. See
                    # ``default_local`` and the ``route_unknown_profile_to_default``
                    # constructor flag for the rationale.
                    row = self._rows_by_name[self._default_name]
                    matched = self._default_name
                else:
                    return PolicyDecision(
                        verdict="deny",
                        matched_profile=self._default_name,
                        deny_reason=(
                            f"policy_profile {policy_profile!r} is not declared in "
                            "the policy table"
                        ),
                    )
            else:
                matched = row.profile_name

        if not row.permits_production_scope(production_scope):
            return PolicyDecision(
                verdict="deny",
                matched_profile=matched,
                deny_reason=(
                    f"production_scope {production_scope!r} is not permitted by "
                    f"profile {matched!r}"
                ),
            )

        if row.require_authorization_decision and authorization_decision is None:
            return PolicyDecision(
                verdict="deny",
                matched_profile=matched,
                deny_reason=(
                    f"profile {matched!r} requires authorization_decision but the "
                    "request did not provide one"
                ),
            )

        if row.approval_ticket_pattern is not None:
            if approval_ticket is None:
                return PolicyDecision(
                    verdict="deny",
                    matched_profile=matched,
                    deny_reason=(
                        f"profile {matched!r} requires an approval_ticket and the "
                        "request did not provide one"
                    ),
                )
            if len(approval_ticket) > _APPROVAL_TICKET_MAX_LEN:
                return PolicyDecision(
                    verdict="deny",
                    matched_profile=matched,
                    deny_reason=(
                        f"approval_ticket exceeds the {_APPROVAL_TICKET_MAX_LEN}-char "
                        "limit"
                    ),
                )
            if re.fullmatch(row.approval_ticket_pattern, approval_ticket) is None:
                return PolicyDecision(
                    verdict="deny",
                    matched_profile=matched,
                    deny_reason=(
                        f"approval_ticket does not match the pattern required by "
                        f"profile {matched!r}"
                    ),
                )

        return PolicyDecision(verdict="permit", matched_profile=matched, deny_reason=None)


__all__: list[str] = [
    "PolicyDecision",
    "RestorationPolicyRow",
    "RestorationPolicyTable",
]


def __getattr__(name: str) -> Any:  # pragma: no cover - trivial
    raise AttributeError(f"module 'yomotsusaka.policy' has no attribute {name!r}")
