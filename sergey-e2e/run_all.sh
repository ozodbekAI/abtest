#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [[ "$#" -eq 0 ]]; then
  set -- F01 F02 F03 F04 F05 F06 F07 F08 F09
fi
"$ROOT/sergey-e2e/stack_local.sh" up
cd "$ROOT/sergey-e2e"
exec "${PYTHON:-$ROOT/venv/bin/python}" rr2.py "$@"
