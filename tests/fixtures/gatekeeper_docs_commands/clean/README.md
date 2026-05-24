# Clean fixture

This fixture passes every check in `scripts/gatekeeper/check_docs_commands.py`.

```sh
mkdir -p ./inbox
uv run python -m yomotsusaka.cli.run_batch ./inbox --vault-root ./vault
```

The fixture path `./inbox` is seeded with `mkdir` before use.
