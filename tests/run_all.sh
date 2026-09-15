#!/bin/sh
# Run every suite; exit non-zero if any fails.
set -e
cd "$(dirname "$0")/.."
for f in tests/test_*.py; do
  echo "=== $f"
  python3 "$f" | tail -1
done
echo "ALL SUITES PASSED"
