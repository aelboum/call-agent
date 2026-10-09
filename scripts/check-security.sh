#!/usr/bin/env bash
# Security validation: no secret may enter the repository.
#
# `detect-secrets scan --baseline FILE` is a generator, not a gate: it always
# exits 0 and, when FILE already exists, silently rewrites it to include
# every current finding -- it never fails on an unreviewed secret. The actual
# fail-closed check is `detect-secrets-hook`: given the baseline plus an
# explicit file list, it diffs live scan results against the baseline by
# (filename, secret type, hashed value) and exits 1 on anything not already
# reviewed there, 0 when clean, and 3 when only line-number bookkeeping in
# the baseline is stale (same secrets, shifted lines -- not a new finding).
# It never prints a secret value, only its type and location.
set -euo pipefail

echo "== detect-secrets (fail-closed gate against reviewed baseline) =="
mapfile -t tracked_files < <(git ls-files)

set +e
detect-secrets-hook --baseline .secrets.baseline "${tracked_files[@]}"
status=$?
set -e

case "$status" in
  0)
    echo "ok: no unreviewed findings"
    ;;
  3)
    echo "ok: no unreviewed findings (baseline line-number metadata only, already-reviewed secrets unchanged)"
    ;;
  *)
    echo "FAIL: detect-secrets found a potential secret that is not in the reviewed .secrets.baseline (see above)." >&2
    exit 1
    ;;
esac

echo "== .env must not be tracked =="
if git ls-files --error-unmatch .env >/dev/null 2>&1; then
  echo "FAIL: .env is tracked by git" >&2
  exit 1
fi
echo "ok"
