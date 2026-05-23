"""Scrubber — fail-closed redaction of free-form text emitted by templates.

Used by :func:`yomotsusaka.boundary.execute_request` (issue #43) to scrub
``stdout``/``stderr`` fragments before they cross the private-boundary
trust zone back to an ordinary-agent caller.

Two-pass model
--------------

1. **Raw-value mask** — for every ``PrivateDictEntry.original_value`` (when
   non-empty) substring-replace each occurrence with the entry's canonical
   ``<KIND_xxxxxxxx>`` placeholder key. ``str.replace`` is used (not regex)
   so behaviour is deterministic across special characters in raw values.
2. **Path-shape mask** — replace any substring matching the vault-shape
   regex (``/manifests/...``, ``/private/...``, ``/audit/...``) with the
   placeholder ``<vault_path>`` so vault layout cannot leak via template
   error messages.

After both passes a **fail-closed re-check** sweeps the output for any
remaining non-empty ``original_value``: if any are still present a
:class:`ScrubError` is raised. The dispatcher converts the error to an
``ExecutionFailureReason.ScrubFailed`` outcome and writes an audit record
before returning.

Reuse rule
----------

This module imports :data:`yomotsusaka.validator._PLACEHOLDER_PATTERN` for
shape recognition only. It does NOT call :meth:`Validator.validate`, which
requires a :class:`DocumentManifest` and is structured around manifest-shaped
data; template stdout/stderr is free-form.
"""

from __future__ import annotations

import re

from yomotsusaka.schemas import PrivateDictEntry
from yomotsusaka.validator import _PLACEHOLDER_PATTERN  # noqa: F401 — reused for shape recognition

logger = __import__("logging").getLogger(__name__)


# Vault-layout path detector. Mirrors the patterns used by
# :data:`tests._exposure_denylist.PATH_LEAK_PATTERNS` (which the exposure
# scan uses to detect leaks in agent-facing serialisations) plus the
# ``/audit/...`` shape that :func:`yomotsusaka.boundary._append_restoration_audit`
# writes through.
_VAULT_PATH_PATTERN = re.compile(
    r"/(?:manifests|private|audit)/[A-Za-z0-9._-]{1,128}\.(?:json|jsonl)"
)


class ScrubError(Exception):
    """Raised by :func:`scrub_stream` when the fail-closed re-check finds
    any raw private value still present after both scrubber passes."""


def scrub_stream(
    text: str,
    private_dict: list[PrivateDictEntry] | tuple[PrivateDictEntry, ...],
) -> str:
    """Scrub *text* against *private_dict* and return the redacted result.

    Performs the raw-value mask + path-shape mask described in the module
    docstring, then re-scans the output for any remaining raw value. On a
    positive hit, raises :class:`ScrubError` with a generic message — the
    raw value itself is NEVER included in the exception message.

    Parameters
    ----------
    text:
        Free-form text to scrub (e.g. captured stdout/stderr from a
        template invocation). Non-``str`` input raises :class:`TypeError`.
    private_dict:
        Sequence of :class:`PrivateDictEntry` carrying the
        ``(original_value, key)`` pairs to substitute. Empty
        ``original_value`` entries are silently skipped.

    Returns
    -------
    str
        The scrubbed text. ``<KIND_xxxxxxxx>`` placeholders replace raw
        values; ``<vault_path>`` replaces any vault-shaped path.

    Raises
    ------
    TypeError
        If *text* is not a string.
    ScrubError
        If the fail-closed re-check finds any non-empty ``original_value``
        still present after both passes. This is a hard fail-closed
        guarantee: the gateway converts it to
        :data:`ExecutionFailureReason.ScrubFailed` and writes an audit
        record before returning the failure.
    """
    if not isinstance(text, str):
        raise TypeError(f"text must be a str; got {type(text).__name__}")

    if not text:
        return ""

    # Pass 1: raw-value mask. Sort by length descending so longer raw
    # values win over shorter prefixes (e.g. "Alice Tan" replaces before
    # "Alice"), matching the QueryResolver's longest-match-first rule.
    entries = list(private_dict)
    entries.sort(key=lambda e: len(e.original_value), reverse=True)

    scrubbed = text
    for entry in entries:
        if not entry.original_value:
            # Empty raw values are non-actionable; skip.
            continue
        scrubbed = scrubbed.replace(entry.original_value, entry.key)

    # Pass 2: path-shape mask. Replace vault-layout-shaped substrings.
    scrubbed = _VAULT_PATH_PATTERN.sub("<vault_path>", scrubbed)

    # Fail-closed re-check. Sweep the output for any non-empty raw value
    # that survived both passes (e.g. due to a Unicode normalisation edge
    # case or a value that overlapped with itself). On a positive hit raise
    # ScrubError with a generic message — the raw value MUST NOT be echoed.
    for entry in entries:
        if entry.original_value and entry.original_value in scrubbed:
            # Privacy-discipline log line: identify the key, not the value.
            logger.warning(
                "scrub_stream fail-closed re-check found a residual raw value "
                "for key %r after both passes",
                entry.key,
            )
            raise ScrubError(
                f"scrubber re-check found residual raw value for key "
                f"{entry.key!r}"
            )

    return scrubbed


__all__ = ["ScrubError", "scrub_stream"]
