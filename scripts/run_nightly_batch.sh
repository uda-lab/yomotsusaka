#!/usr/bin/env bash
# run_nightly_batch.sh — trigger a nightly redaction batch.
#
# Usage:
#   ./scripts/run_nightly_batch.sh [DOC_DIR]
#
# DOC_DIR defaults to ./inbox if not provided.

set -euo pipefail

DOC_DIR="${1:-./inbox}"

echo "[$(date -u +%FT%TZ)] Starting nightly batch for $DOC_DIR"

# Activate the project virtual environment if present
if [ -f ".venv/bin/activate" ]; then
    # shellcheck source=/dev/null
    source .venv/bin/activate
fi

python -m yomotsusaka.cli.run_batch "$DOC_DIR" --vault-root "${VAULT_ROOT:-./vault}"

echo "[$(date -u +%FT%TZ)] Nightly batch script finished."
