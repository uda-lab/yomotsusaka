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

python - "$DOC_DIR" <<'EOF'
import sys
from pathlib import Path
from yomotsusaka.batch_queue import BatchQueue

doc_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("./inbox")
doc_refs = [str(p) for p in doc_dir.glob("**/*") if p.is_file()]

if not doc_refs:
    print("No documents found — nothing to do.")
    sys.exit(0)

queue = BatchQueue()
batch = queue.submit(doc_refs)
print(f"Submitted batch {batch.batch_id} with {len(doc_refs)} documents.")
print("Batch processing is not yet automated — implement pipeline wiring here.")
EOF

echo "[$(date -u +%FT%TZ)] Nightly batch script finished."
