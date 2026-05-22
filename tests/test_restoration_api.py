"""Tests for restoration behavior."""

from pathlib import Path

from yomotsusaka.commit import commit
from yomotsusaka.restoration_api import restore
from yomotsusaka.schemas import DocumentManifest, EntityKind, PrivateDictEntry


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
    restored = restore(handle, vault_root=tmp_path / "wrong-root")

    assert len(restored) == 1
    assert restored[0].original_value == "Alice"
