#!/usr/bin/env bash
# Security validation: no secret may enter the repository.
set -euo pipefail

echo "== detect-secrets (scan against baseline) =="
detect-secrets scan --baseline .secrets.baseline

echo "== .env must not be tracked =="
if git ls-files --error-unmatch .env >/dev/null 2>&1; then
  echo "FAIL: .env is tracked by git" >&2
  exit 1
fi
echo "ok"
