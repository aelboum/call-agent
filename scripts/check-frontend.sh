#!/usr/bin/env bash
# Frontend validation.
set -euo pipefail

cd "$(dirname "$0")/../frontend"
npm run typecheck
npm run build
