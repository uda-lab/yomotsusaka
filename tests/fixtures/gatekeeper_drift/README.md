# Drift fixtures

Each `.md` in this directory seeds one negative test case for
`tests/test_gatekeeper_docs_links.py` or
`tests/test_gatekeeper_vocab_drift.py`. They are NOT scanned by the
production gate-keeper invocation (which scans the repo's real
`README.md`, `AGENTS.md`, and `docs/*.md`).

The test harness builds a temporary repo skeleton, copies the chosen
fixture into the synthetic doc set, and invokes the script's
`check_*` functions directly so the failure-mode assertions are
deterministic and never depend on `gh` connectivity.
