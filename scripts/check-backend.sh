#!/usr/bin/env bash
# Backend validation. CI runs this exact script, so there is no separate
# local-vs-CI definition of "passing" to keep in sync.
set -euo pipefail

# import-linter renders its progress spinner through `rich`, which crashes on
# Windows when stdout is redirected and the console codepage is cp1252. UTF-8
# output is correct everywhere and costs nothing on Linux/CI.
export PYTHONIOENCODING=utf-8

echo "== ruff check =="
ruff check .

echo "== ruff format --check =="
ruff format --check .

echo "== pyright =="
pyright

echo "== pytest =="
pytest

echo "== lint-imports (architecture boundaries) =="
lint-imports
