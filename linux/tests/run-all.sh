#!/usr/bin/env bash
# Run every test in this directory and exit non-zero on the first failure.
# Browser tests (test_crypto.html) need a running HTTP server + Playwright;
# they are NOT run by this script — see tests/test_crypto.html for manual
# instructions.

set -u
cd "$(dirname "$0")/.."

fail=0
run() {
  echo
  echo "=== $1 ==="
  if ! python3 "$1"; then
    fail=1
    echo "✗ $1 FAILED"
  fi
}

run tests/test_crypto.py
run tests/test_keystore.py
run tests/test_config_gate.py
run tests/test_e2e.py
run tests/test_resilience.py
run tests/test_multi_instance.py
run tests/test_push.py
run tests/test_wrapper_e2e.py

echo
if [ "$fail" -eq 0 ]; then
  echo "ALL PYTHON TESTS PASSED"
  echo
  echo "Browser-side cross-language vectors (manual):"
  echo "  python3 tests/test_crypto.py --gen-vectors"
  echo "  python3 -m http.server 7755 --bind 127.0.0.1 &"
  echo "  open http://127.0.0.1:7755/tests/test_crypto.html"
  echo "  page title shows OK <pass>/<total>"
else
  echo "TESTS FAILED"
  exit 1
fi
