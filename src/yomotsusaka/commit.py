"""
Commit — persist a processed manifest and return an artifact handle.

In the MVP the vault is a local directory.  A real implementation would write
to encrypted storage and record the mapping in a durable index.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from yomotsusaka.schemas import ArtifactHandle, DocumentManifest, PrivateDictEntry

logger = logging.getLogger(__name__)

_DEFAULT_VAULT = Path(".vault")


def commit(
    manifest: DocumentManifest,
    private_dict: list[PrivateDictEntry],
    vault_root: Path = _DEFAULT_VAULT,
) -> ArtifactHandle:
    """
    Persist *manifest* and *private_dict* to the vault and return a handle.

    The manifest is written to ``<vault_root>/manifests/<doc_id>.json``.
    The private dictionary is written to ``<vault_root>/private/<doc_id>.json``.

    Parameters
    ----------
    manifest:
        Redacted document manifest (agent-safe).
    private_dict:
        Private key→value mappings.  Kept behind the vault boundary.
    vault_root:
        Root directory of the local vault.  Defaults to ``.vault/``.

    Returns
    -------
    ArtifactHandle
        Opaque handle referencing the committed document.
    """
    vault_root.mkdir(parents=True, exist_ok=True)
    manifests_dir = vault_root / "manifests"
    private_dir = vault_root / "private"
    manifests_dir.mkdir(exist_ok=True)
    private_dir.mkdir(exist_ok=True)

    manifest_path = manifests_dir / f"{manifest.doc_id}.json"
    manifest_path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")

    private_path = private_dir / f"{manifest.doc_id}.json"
    entries = [e.model_dump(mode="json") for e in private_dict]
    private_path.write_text(json.dumps(entries, indent=2), encoding="utf-8")

    handle = ArtifactHandle(
        doc_id=manifest.doc_id,
        vault_path=str(private_path),
    )
    logger.info("Committed doc %s → handle %s", manifest.doc_id, handle.handle_id)
    return handle
