"""
Restoration API — controlled re-hydration of private values.

Agents must request restoration through this API; they never receive raw
private data directly.  Access-control hooks should be added here before
production use.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from yomotsusaka.schemas import ArtifactHandle, PrivateDictEntry

logger = logging.getLogger(__name__)

_DEFAULT_VAULT = Path(".vault")


class RestorationError(Exception):
    """Raised when private data cannot be retrieved."""


def restore(
    handle: ArtifactHandle,
    vault_root: Path = _DEFAULT_VAULT,
) -> list[PrivateDictEntry]:
    """
    Return the private dictionary for a committed document.

    Parameters
    ----------
    handle:
        Artifact handle returned by :func:`~yomotsusaka.commit.commit`.
    vault_root:
        Root directory of the local vault.

    Returns
    -------
    list[PrivateDictEntry]
        The private key→value mappings for the document.

    Raises
    ------
    RestorationError
        If the vault file cannot be found or read.
    """
    private_path = vault_root / "private" / f"{handle.doc_id}.json"
    if not private_path.exists():
        raise RestorationError(f"No private data found for doc {handle.doc_id}")

    raw = json.loads(private_path.read_text(encoding="utf-8"))
    entries = [PrivateDictEntry(**item) for item in raw]
    logger.info("Restored %d entries for doc %s", len(entries), handle.doc_id)
    return entries
