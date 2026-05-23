"""
``python -m yomotsusaka.cli.run_batch`` — drain an inbox through the facade.

Wraps :class:`yomotsusaka.batch_runner.BatchRunner` for shell invocation.

CLI surface
-----------

    python -m yomotsusaka.cli.run_batch <inbox_dir>
        --vault-root <path>
        [--tenant-id <id>]
        [--fail-on-error | --no-fail-on-error]

Privacy invariants (binding)
----------------------------
Standard output never carries raw input text, manifest bodies, or vault
filesystem paths. On success the CLI prints exactly one summary line of the
shape::

    batch <batch_id> committed=<N> failed=<M>

On failure the CLI prints a short diagnostic to stderr (paths supplied by
the caller are echoed; they are caller-public for MVP per the child_06 spec)
and exits non-zero.

The CLI does not import any private-kernel module directly. All pipeline
access is mediated by :class:`yomotsusaka.facade.LocalFacade`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from yomotsusaka.batch_runner import BatchRunner, BatchSummary
from yomotsusaka.facade import LocalFacade
from yomotsusaka.tenant import TenantScope


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m yomotsusaka.cli.run_batch",
        description=(
            "Drain an inbox directory of raw documents through the local "
            "facade pipeline. Each document is redacted, validated, "
            "committed to the vault, and indexed in the facade's search "
            "gateway."
        ),
    )
    parser.add_argument(
        "inbox_dir",
        type=Path,
        help="Directory containing raw text documents (recursively walked).",
    )
    parser.add_argument(
        "--vault-root",
        type=Path,
        required=True,
        help="Vault root for committed manifests and private dictionaries.",
    )
    parser.add_argument(
        "--tenant-id",
        type=str,
        default=None,
        help=(
            "Optional tenant id. When supplied the facade is constructed "
            "with TenantScope(tenant_id=..., vault_root=...); otherwise "
            "the back-compat local scope is used."
        ),
    )
    parser.add_argument(
        "--fail-on-error",
        dest="fail_on_error",
        action="store_true",
        default=True,
        help="Exit non-zero if any document failed (default).",
    )
    parser.add_argument(
        "--no-fail-on-error",
        dest="fail_on_error",
        action="store_false",
        help="Always exit zero even if some documents failed.",
    )
    return parser


def _construct_facade(vault_root: Path, tenant_id: str | None) -> LocalFacade:
    if tenant_id is not None:
        tenant = TenantScope(tenant_id=tenant_id, vault_root=vault_root)
        return LocalFacade(tenant=tenant)
    return LocalFacade(vault_root)


def _emit_summary(summary: BatchSummary) -> None:
    # Single-line, sanitised summary. No batch id is ever derived from a
    # raw document path, so this line carries no private values; the
    # batch_id is a UUID4 produced by ``BatchQueue.submit``.
    sys.stdout.write(
        f"batch {summary.batch_id} "
        f"committed={summary.committed_count} "
        f"failed={summary.failed_count}\n"
    )
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns the process exit code.

    Exit codes
    ----------
    0
        Batch processed successfully. With ``--fail-on-error`` this requires
        zero per-document failures; with ``--no-fail-on-error`` any outcome
        with no infrastructure error qualifies.
    1
        Infrastructure failure (inbox missing, vault root not writable, or
        an uncaught runner exception).
    2
        Batch completed but had at least one per-document failure and
        ``--fail-on-error`` was in effect.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    inbox: Path = args.inbox_dir
    vault_root: Path = args.vault_root

    if not inbox.exists():
        sys.stderr.write(f"error: inbox directory does not exist: {inbox}\n")
        return 1
    if not inbox.is_dir():
        sys.stderr.write(f"error: inbox is not a directory: {inbox}\n")
        return 1

    # Probe vault-root writability up front so an unwritable vault produces
    # a clean diagnostic instead of a half-processed batch. ``mkdir`` is
    # idempotent and matches what ``commit.commit`` does internally; doing
    # it here keeps the CLI's failure modes deterministic.
    try:
        vault_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        sys.stderr.write(
            f"error: vault root not writable ({vault_root}): "
            f"{type(exc).__name__}\n"
        )
        return 1

    try:
        facade = _construct_facade(vault_root, args.tenant_id)
    except (ValueError, TypeError) as exc:
        # TenantScope rejects malformed tenant ids; surface the rejection
        # without echoing internal state.
        sys.stderr.write(f"error: {exc}\n")
        return 1

    runner = BatchRunner(facade=facade)

    try:
        summary = runner.run_directory(inbox)
    except (FileNotFoundError, NotADirectoryError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1
    except Exception as exc:  # pragma: no cover - defensive
        # The runner already swallows per-document failures; any exception
        # surfacing here is a programmer error and should not be silently
        # masked behind a zero exit code. We log the type only, not the
        # message — pydantic ValidationError messages may echo input.
        sys.stderr.write(f"error: runner aborted ({type(exc).__name__})\n")
        return 1

    _emit_summary(summary)

    if args.fail_on_error and summary.failed_count > 0:
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
