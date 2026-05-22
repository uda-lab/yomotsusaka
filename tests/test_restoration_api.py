"""Tests for restoration behavior."""

from pathlib import Path

import pytest

from yomotsusaka.commit import commit
from yomotsusaka.restoration_api import RestorationError, restore
from yomotsusaka.schemas import ArtifactHandle, DocumentManifest, EntityKind, PrivateDictEntry


def test_restore_uses_handle_vault_path(tmp_path: Path):
    vault_root = tmp_path / "custom-vault"
    manifest = DocumentManifest(source_ref="sha256:test", redacted_text="Hello <PERSON_x>.")
    private_dict = [
        PrivateDictEntry(
            key="<PERSON_x>",
            original_value="Alice",
            kind=EntityKind.PERSON,
        )
    ]

    handle = commit(manifest, private_dict, vault_root=vault_root)
    restored = restore(handle, vault_root=vault_root)

    assert len(restored) == 1
    assert restored[0].original_value == "Alice"


def test_restore_rejects_path_outside_vault(tmp_path: Path):
    forged = ArtifactHandle(
        doc_id="doc-001",
        vault_path=str(tmp_path / "outside.json"),
    )

    with pytest.raises(RestorationError):
        restore(forged, vault_root=tmp_path / "vault")
