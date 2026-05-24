# Drift: malformed_fence

This file has an unclosed fence which must trigger an internal-error
exit (code 2).

```sh
echo "this fence never closes"
echo "and the script must report it as an internal error"
