#!/usr/bin/env bash
# Everything that does not need Docker or a live database.
set -euo pipefail
here="$(dirname "$0")"
bash "$here/check-backend.sh"
bash "$here/check-security.sh"
bash "$here/check-frontend.sh"
