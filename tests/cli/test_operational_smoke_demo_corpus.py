"""Tests for the ``operational_smoke --demo-corpus`` flag (issue #113 / F5).

The flag materialises a tiny canonical-fixture inbox under a fresh temp
directory so the agent quickstart command works from a clean checkout
without owner-supplied inbox content.

Coverage:

* end-to-end happy path (all required phases ``ok``, RunPod skipped);
* cleanup of the temp directory on normal exit;
* opt-in retention via ``--keep-demo-corpus`` with the stderr advisory;
* invariant that the positional inbox path is never mutated;
* usage error when ``--keep-demo-corpus`` is passed without ``--demo-corpus``;
* anti-drift guard: the shipped package-data files match the canonical
  test fixtures byte-for-byte.

These tests run the CLI in-process via ``cli_mod.main`` where possible so
``monkeypatch`` can intercept :func:`tempfile.mkdtemp`. The end-to-end
test (which must exercise the full subprocess path including the phase
3/4 child program) spawns the CLI via ``python -m`` like the sibling
test module does.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from importlib import resources
from pathlib import Path

import pytest


def _run_cli(
    args: list[str], *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env_full = dict(os.environ) if env is None else env
    # Match the sibling test module's contract: the no-network default
    # is meaningful only when RUNPOD_API_KEY is absent.
    env_full.pop("RUNPOD_API_KEY", None)
    return subprocess.run(
        [sys.executable, "-m", "yomotsusaka.cli.operational_smoke", *args],
        capture_output=True,
        text=True,
        check=False,
        env=env_full,
    )


def _parse_phase_lines(stdout: str) -> list[tuple[str, str, str]]:
    pat = re.compile(
        r"^phase=(?P<phase>\S+) status=(?P<status>\S+) "
        r"category=(?P<category>\S+)$"
    )
    out: list[tuple[str, str, str]] = []
    for line in stdout.splitlines():
        m = pat.match(line)
        if m:
            out.append((m["phase"], m["status"], m["category"]))
    return out


def _result_line(stdout: str) -> str | None:
    for line in stdout.splitlines():
        if line.startswith("result="):
            return line[len("result=") :]
    return None


# ---------------------------------------------------------------------------
# End-to-end happy path
# ---------------------------------------------------------------------------


def test_demo_corpus_runs_clean(tmp_path: Path) -> None:
    """The acceptance criterion for #113: a fresh checkout + the
    documented quickstart command must produce ``result=completed``.

    Passing a positional inbox path that does NOT exist confirms the
    demo-corpus flag drives the inbox selection independently of the
    positional argument — i.e. the quickstart cannot accidentally pick
    up a stale on-disk inbox.
    """
    unused_inbox = tmp_path / "unused-inbox-does-not-exist"
    vault = tmp_path / "vault"

    result = _run_cli(
        [str(unused_inbox), "--vault-root", str(vault), "--demo-corpus"]
    )
    assert result.returncode == 0, (
        f"exit={result.returncode}; stdout={result.stdout!r}; "
        f"stderr={result.stderr!r}"
    )

    phases = _parse_phase_lines(result.stdout)
    statuses = {p: (s, c) for p, s, c in phases}
    assert statuses["batch"] == ("ok", "batch_committed")
    assert statuses["index_snapshot"] == ("ok", "snapshot_written")
    assert statuses["index_reload"] == ("ok", "index_reloaded")
    assert statuses["search_smoke"] == ("ok", "hits_found")
    assert statuses["restoration_request"] == (
        "ok",
        "restoration_request_recorded",
    )
    assert statuses["audit_inspect"] == ("ok", "audit_present")
    assert statuses["runpod_lifecycle"] == ("skipped", "runpod_disabled")
    assert _result_line(result.stdout) == "completed"

    # Override advisory lands on STDERR, never STDOUT.
    assert "notice=demo_corpus_override" in result.stderr
    for line in result.stdout.splitlines():
        assert "notice=" not in line, (
            f"stderr advisory leaked onto stdout: {line!r}"
        )


# ---------------------------------------------------------------------------
# Cleanup semantics
# ---------------------------------------------------------------------------


def test_demo_corpus_cleans_up_temp_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On normal exit the temp directory must be removed."""
    from yomotsusaka.cli import operational_smoke as cli_mod

    known_temp = tmp_path / "fake-temp-demo-corpus"

    def fake_mkdtemp(prefix: str = "tmp") -> str:
        assert prefix == cli_mod._DEMO_CORPUS_TEMP_PREFIX
        known_temp.mkdir(parents=True, exist_ok=False)
        return str(known_temp)

    monkeypatch.setattr(tempfile, "mkdtemp", fake_mkdtemp)

    vault = tmp_path / "vault"
    exit_code = cli_mod.main(
        [
            str(tmp_path / "unused"),
            "--vault-root",
            str(vault),
            "--demo-corpus",
        ]
    )
    assert exit_code == 0
    assert not known_temp.exists(), (
        "demo-corpus temp dir must be removed on normal exit"
    )


def test_demo_corpus_keeps_temp_dir_when_flag_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--keep-demo-corpus`` skips cleanup and echoes a stderr notice
    with the retained path. The path is allowed on stderr only.
    """
    from yomotsusaka.cli import operational_smoke as cli_mod

    known_temp = tmp_path / "fake-kept-demo-corpus"

    def fake_mkdtemp(prefix: str = "tmp") -> str:
        known_temp.mkdir(parents=True, exist_ok=False)
        return str(known_temp)

    monkeypatch.setattr(tempfile, "mkdtemp", fake_mkdtemp)

    vault = tmp_path / "vault"
    try:
        exit_code = cli_mod.main(
            [
                str(tmp_path / "unused"),
                "--vault-root",
                str(vault),
                "--demo-corpus",
                "--keep-demo-corpus",
            ]
        )
        captured = capsys.readouterr()
        assert exit_code == 0
        assert known_temp.exists(), (
            "demo-corpus temp dir must persist with --keep-demo-corpus"
        )
        # Stable token + path on STDERR; no demo-corpus notice on stdout.
        assert "notice=demo_corpus_kept" in captured.err
        assert str(known_temp) in captured.err
        for line in captured.out.splitlines():
            assert "notice=" not in line
            assert str(known_temp) not in line
    finally:
        # Test housekeeping — the kept dir is normally an owner artifact.
        if known_temp.exists():
            import shutil as _shutil

            _shutil.rmtree(known_temp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Positional inbox preserved
# ---------------------------------------------------------------------------


def test_demo_corpus_does_not_mutate_positional_inbox(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The positional ``inbox_dir`` must be byte-identical after a
    ``--demo-corpus`` run, even when the path exists and contains files.
    """
    from yomotsusaka.cli import operational_smoke as cli_mod

    positional_inbox = tmp_path / "positional-inbox"
    positional_inbox.mkdir()
    sentinel = positional_inbox / "do-not-touch.txt"
    sentinel_bytes = b"this content must be left untouched by --demo-corpus"
    sentinel.write_bytes(sentinel_bytes)
    sibling_count_before = len(list(positional_inbox.iterdir()))

    vault = tmp_path / "vault"
    exit_code = cli_mod.main(
        [
            str(positional_inbox),
            "--vault-root",
            str(vault),
            "--demo-corpus",
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    # Positional path content untouched.
    assert sentinel.read_bytes() == sentinel_bytes
    assert len(list(positional_inbox.iterdir())) == sibling_count_before
    # Override advisory recorded on stderr.
    assert "notice=demo_corpus_override" in captured.err


# ---------------------------------------------------------------------------
# Usage error: --keep-demo-corpus without --demo-corpus
# ---------------------------------------------------------------------------


def test_demo_corpus_keep_without_demo_errors(tmp_path: Path) -> None:
    """``--keep-demo-corpus`` alone is a usage error (argparse exit 2)."""
    result = _run_cli(
        [
            str(tmp_path / "inbox"),
            "--vault-root",
            str(tmp_path / "vault"),
            "--keep-demo-corpus",
        ]
    )
    assert result.returncode != 0
    assert "--keep-demo-corpus requires --demo-corpus" in result.stderr


# ---------------------------------------------------------------------------
# Anti-drift guard
# ---------------------------------------------------------------------------


def test_demo_corpus_shipped_files_match_test_fixtures() -> None:
    """The package-data copies under ``src/yomotsusaka/cli/_demo_corpus/``
    must be byte-identical to the canonical fixtures under
    ``tests/fixtures/redaction_corpus/``. The shipped copies exist
    because the test fixtures are not packaged; this assertion is the
    sole guard against the mirror silently drifting.
    """
    fixtures_root = (
        Path(__file__).resolve().parent.parent
        / "fixtures"
        / "redaction_corpus"
    )
    shipped = resources.files("yomotsusaka.cli._demo_corpus")
    for name in ("canonical_employee.txt", "multi_mention.txt"):
        shipped_bytes = (shipped / name).read_bytes()
        fixture_bytes = (fixtures_root / name).read_bytes()
        assert shipped_bytes == fixture_bytes, (
            f"demo-corpus shipped file {name!r} has drifted from the "
            f"canonical test fixture; re-sync src/yomotsusaka/cli/"
            f"_demo_corpus/ from tests/fixtures/redaction_corpus/"
        )
