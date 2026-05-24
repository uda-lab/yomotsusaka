# Drift: tee_pipefail_guard

```bash
uv run pytest 2>&1 | tee pytest.log
if [ $? -ne 0 ]; then
  echo "tests failed"
fi
```
