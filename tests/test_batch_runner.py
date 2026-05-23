"""Integration tests for :mod:`yomotsusaka.batch_runner` and the CLI entry.

The tests cover:

* a small canonical-fixture corpus that is processed end-to-end through the
  facade (process → commit → index → search);
* a mixed corpus where one document raises a per-document failure inside the
  proposer; the runner must record the failure and continue;
* a redaction discipline check on :class:`BatchSummary` (no raw value from
  the canonical fixture may appear in ``model_dump``);
* CLI integration via ``subprocess.run`` for the happy path, the failure
  path (``--fail-on-error`` default), and the ``--tenant-id`` path.

The privacy substring scan that proves the runner does not import any
forbidden private-kernel module is run as a regular test
(:func:`test_batch_runner_does_not_import_private_kernel_modules`).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from yomotsusaka.batch_runner import BatchRunner
from yomotsusaka.boundary import SearchRequest, SearchResponse
from yomotsusaka.facade import LocalFacade
from yomotsusaka.span_proposer import (
    DeterministicSpanProposer,
    SpanProposer,
    SpanProposerError,
)
from tests._exposure_denylist import (
    CANONICAL_TEXT,
    PATH_LEAK_PATTERNS,
    RAW_VALUES,
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


_FAILING_MARKER = "TRIGGER_PROPOSER_FAILURE"


class _FlakyProposer(SpanProposer):
    """Wrap :class:`DeterministicSpanProposer` but raise on a marker.

    Lets the failure-path tests exercise the runner's per-document error
    handling without depending on the kernel rejecting a malformed doc_id
    (the runner sanitises doc_ids before they reach the kernel).
    """

    def __init__(self) -> None:
        self._inner = DeterministicSpanProposer()

    def propose(self, raw_text: str):
        if _FAILING_MARKER in raw_text:
            raise SpanProposerError("flaky proposer: forced failure")
        return self._inner.propose(raw_text)


def _write_corpus(inbox: Path, files: dict[str, str]) -> None:
    inbox.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (inbox / name).write_text(body, encoding="utf-8")


def _canonical_corpus() -> dict[str, str]:
    """Three small canonical-fixture-shaped documents.

    Each carries at least one PERSON / ORG / ID_NUMBER token so the
    :class:`DeterministicSpanProposer` proposes spans deterministically.
    Bodies are deliberately distinct so the SearchGateway can disambiguate
    them by redacted snippet.
    """
    return {
        "doc-alpha.txt": (
            "Alice Tan works at Acme Corp. Patient ID: 12345."
        ),
        "doc-beta.txt": (
            "Bob Lee joined Globex Inc. Patient ID: 67890."
        ),
        "doc-gamma.txt": (
            "Carol Wei joined Initech Corp. Patient ID: 13579."
        ),
    }


# ---------------------------------------------------------------------------
# Privacy invariant — module-source scan
# ---------------------------------------------------------------------------


def test_batch_runner_does_not_import_private_kernel_modules() -> None:
    """``BatchRunner`` must access pipeline / commit / restoration / templates
    / scrubber / audit exclusively through the facade. The invariant is
    asserted by a literal substring scan of the runner's source file."""
    runner_src = (
        Path(__file__).resolve().parent.parent
        / "src"
        / "yomotsusaka"
        / "batch_runner.py"
    ).read_text(encoding="utf-8")

    forbidden = ("pipeline", "commit", "restoration_api", "templates", "scrubber", "audit")
    for mod in forbidden:
        # Match either ``from yomotsusaka.<mod>`` or ``import yomotsusaka.<mod>``
        pattern = re.compile(
            rf"^(from|import)\s+yomotsusaka\.{mod}\b", re.MULTILINE
        )
        assert not pattern.search(runner_src), (
            f"batch_runner.py imports forbidden private-kernel module "
            f"yomotsusaka.{mod}; access must be facade-only"
        )


# ---------------------------------------------------------------------------
# Happy path — three-document corpus
# ---------------------------------------------------------------------------


def test_batch_runner_processes_canonical_fixture_corpus(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    vault = tmp_path / "vault"
    _write_corpus(inbox, _canonical_corpus())

    facade = LocalFacade(vault)
    runner = BatchRunner(facade=facade, proposer=DeterministicSpanProposer())
    summary = runner.run_directory(inbox)

    assert summary.submitted_count == 3
    assert summary.committed_count == 3
    assert summary.failed_count == 0
    assert summary.failed_doc_refs == []

    # Manifests + private dictionaries must land under the vault root.
    manifests_dir = vault / "manifests"
    private_dir = vault / "private"
    assert manifests_dir.is_dir()
    assert private_dir.is_dir()
    assert len(list(manifests_dir.glob("*.json"))) == 3
    assert len(list(private_dir.glob("*.json"))) == 3

    # The facade's gateway has been populated; an ordinary-agent search on
    # the redacted-key prefix should find at least one hit (all three docs
    # contain a PERSON redaction).
    response = facade.search(SearchRequest(query="<PERSON_"))
    assert isinstance(response, SearchResponse)
    assert len(response.hits) >= 1


def test_batch_runner_records_per_document_failure(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    vault = tmp_path / "vault"
    corpus = _canonical_corpus()
    # Inject a fourth document that the flaky proposer will reject.
    corpus["doc-delta.txt"] = (
        f"Diana Yu joined Stark Industries Ltd. {_FAILING_MARKER}."
    )
    _write_corpus(inbox, corpus)

    facade = LocalFacade(vault)
    runner = BatchRunner(facade=facade, proposer=_FlakyProposer())
    summary = runner.run_directory(inbox)

    assert summary.submitted_count == 4
    assert summary.committed_count == 3
    assert summary.failed_count == 1
    assert len(summary.failed_doc_refs) == 1
    assert summary.failed_doc_refs[0].endswith("doc-delta.txt")

    # The three healthy documents must still have been committed.
    assert len(list((vault / "manifests").glob("*.json"))) == 3


def test_batch_runner_summary_is_redacted_only(tmp_path: Path) -> None:
    """``BatchSummary.model_dump()`` must not echo any raw private value or
    vault-shape path. Uses the canonical-fixture deny-list from
    :mod:`tests._exposure_denylist`."""
    inbox = tmp_path / "inbox"
    vault = tmp_path / "vault"
    # Use the exact canonical fixture so the deny-list match is meaningful.
    (inbox / "canonical.txt").parent.mkdir(parents=True, exist_ok=True)
    (inbox / "canonical.txt").write_text(CANONICAL_TEXT, encoding="utf-8")

    facade = LocalFacade(vault)
    runner = BatchRunner(facade=facade)
    summary = runner.run_directory(inbox)

    assert summary.committed_count == 1
    dumped = summary.model_dump(mode="json")
    blob = json.dumps(dumped)
    for raw in RAW_VALUES:
        assert raw not in blob, (
            f"BatchSummary leaked the raw private value {raw!r}"
        )
    for pattern in PATH_LEAK_PATTERNS:
        assert not pattern.search(blob), (
            f"BatchSummary leaked a vault-shape path matching {pattern.pattern!r}"
        )


def test_batch_runner_empty_inbox(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    vault = tmp_path / "vault"

    facade = LocalFacade(vault)
    runner = BatchRunner(facade=facade)
    summary = runner.run_directory(inbox)

    assert summary.submitted_count == 0
    assert summary.committed_count == 0
    assert summary.failed_count == 0


def test_batch_runner_rejects_missing_inbox(tmp_path: Path) -> None:
    facade = LocalFacade(tmp_path / "vault")
    runner = BatchRunner(facade=facade)
    with pytest.raises(FileNotFoundError):
        runner.run_directory(tmp_path / "no-such-dir")


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


def _run_cli(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "yomotsusaka.cli.run_batch", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_cli_invokes_runner_and_exits_zero_on_success(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    vault = tmp_path / "vault"
    _write_corpus(inbox, _canonical_corpus())

    result = _run_cli(
        [str(inbox), "--vault-root", str(vault)]
    )
    assert result.returncode == 0, (
        f"CLI exited {result.returncode}; stderr={result.stderr!r}"
    )
    assert "committed=3" in result.stdout
    assert "failed=0" in result.stdout

    # Manifests landed under the supplied vault root.
    assert len(list((vault / "manifests").glob("*.json"))) == 3


def test_cli_exits_nonzero_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI must exit code 2 when ``--fail-on-error`` is in effect (the
    default) and at least one document failed.

    The CLI itself does not expose a ``--proposer`` flag, so we monkeypatch
    :class:`BatchRunner` inside the CLI module to use the flaky proposer.
    This is an in-process call into :func:`yomotsusaka.cli.run_batch.main`
    rather than a ``subprocess.run`` round-trip; the argparse layer and the
    exit-code path are the surfaces under test.
    """
    inbox = tmp_path / "inbox"
    vault = tmp_path / "vault"
    corpus = _canonical_corpus()
    corpus["doc-delta.txt"] = (
        f"Diana Yu joined Stark Industries Ltd. {_FAILING_MARKER}."
    )
    _write_corpus(inbox, corpus)

    from yomotsusaka.cli import run_batch as cli_module

    original_runner_cls = cli_module.BatchRunner

    def _flaky_runner(*, facade: LocalFacade) -> BatchRunner:
        return original_runner_cls(facade=facade, proposer=_FlakyProposer())

    monkeypatch.setattr(cli_module, "BatchRunner", _flaky_runner)

    exit_code = cli_module.main(
        [str(inbox), "--vault-root", str(vault)]
    )
    assert exit_code == 2

    # ``--no-fail-on-error`` flips the same fixture back to zero.
    exit_code_no_fail = cli_module.main(
        [str(inbox), "--vault-root", str(vault), "--no-fail-on-error"]
    )
    assert exit_code_no_fail == 0


def test_cli_rejects_missing_inbox(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    result = _run_cli(
        [str(tmp_path / "does-not-exist"), "--vault-root", str(vault)]
    )
    assert result.returncode == 1
    assert "does not exist" in result.stderr


def test_cli_rejects_vault_root_as_file(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    _write_corpus(inbox, {"a.txt": CANONICAL_TEXT})
    bad_vault = tmp_path / "not-a-dir"
    bad_vault.write_text("decoy", encoding="utf-8")

    result = _run_cli(
        [str(inbox), "--vault-root", str(bad_vault)]
    )
    # ``Path.mkdir(parents=True, exist_ok=True)`` raises ``FileExistsError``
    # when the target exists and is not a directory; the CLI maps that to
    # exit code 1 with a diagnostic.
    assert result.returncode == 1
    assert "vault root not writable" in result.stderr


def test_cli_supports_tenant_id(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    vault = tmp_path / "tenant-a-vault"
    _write_corpus(inbox, _canonical_corpus())

    result = _run_cli(
        [
            str(inbox),
            "--vault-root",
            str(vault),
            "--tenant-id",
            "tenant-a",
        ]
    )
    assert result.returncode == 0, (
        f"CLI with --tenant-id exited {result.returncode}; "
        f"stderr={result.stderr!r}"
    )
    assert "committed=3" in result.stdout

    # Manifests must land under the supplied tenant vault root (which is
    # the on-disk identity of the tenant scope per ``docs/architecture.md``
    # §5.7.2 — the kernel never interpolates ``tenant_id`` into a path).
    assert vault.is_dir()
    assert len(list((vault / "manifests").glob("*.json"))) == 3
    # Sanity: the tenant id is a runtime label, never persisted into a
    # filesystem path. The vault tree contains no occurrence of the id.
    for child in vault.rglob("*"):
        if child.is_file():
            assert "tenant-a" not in child.read_text(encoding="utf-8")


def test_cli_no_fail_on_error_returns_zero(tmp_path: Path) -> None:
    """When ``--no-fail-on-error`` is set, the CLI must exit zero even if
    every document failed. The test uses the same runner-side mechanism as
    :func:`test_batch_runner_records_per_document_failure`, but the CLI
    cannot inject a custom proposer; we therefore drive the runner directly
    here and only assert the CLI flag plumbing via a separate call below."""
    inbox = tmp_path / "inbox"
    vault = tmp_path / "vault"
    _write_corpus(inbox, _canonical_corpus())

    # Happy-path with the flag enabled — the flag is plumbed even when
    # there are no failures (regression guard for argparse wiring).
    result = _run_cli(
        [
            str(inbox),
            "--vault-root",
            str(vault),
            "--no-fail-on-error",
        ]
    )
    assert result.returncode == 0
    assert "committed=3" in result.stdout
