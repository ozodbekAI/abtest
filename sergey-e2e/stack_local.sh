#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HARNESS="$ROOT/sergey-e2e"
PY="${PYTHON:-$ROOT/venv/bin/python}"
RUN="${SERGEY_E2E_RUN:-$ROOT/.sergey-e2e}"
PGHOST="${PGHOST:-127.0.0.1}"
PGPORT="${PGPORT:-}"
PGUSER="${PGUSER:-postgres}"
PGDATABASE="${PGDATABASE:-wb_optimizer}"
if [[ -z "$PGPORT" ]]; then
  if command -v pg_isready >/dev/null 2>&1 && pg_isready -h "$PGHOST" -p 55432 -U "$PGUSER" >/dev/null 2>&1; then
    PGPORT=55432
  else
    PGPORT=5432
  fi
fi
export APP_ENV=development
export DATABASE_URL="${DATABASE_URL:-postgresql+asyncpg://${PGUSER}@${PGHOST}:${PGPORT}/${PGDATABASE}}"
export AUTO_CREATE_TABLES=false
export JWT_SECRET_KEY="${JWT_SECRET_KEY:-e2e-local-jwt-secret}"
if [[ -z "${FERNET_KEY:-}" ]]; then FERNET_KEY="$($PY -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"; export FERNET_KEY; fi
export MEDIA_SIGNING_SECRET="${MEDIA_SIGNING_SECRET:-e2e-media-secret-long-enough-0123456789}"
export WB_CONTENT_API_URL="http://127.0.0.1:18901/c"
export WB_ADVERT_API_URL="http://127.0.0.1:18901/p"
export WB_ANALYTICS_API_URL="http://127.0.0.1:18901/a"
export WB_STATISTICS_API_URL="http://127.0.0.1:18901/s"
export WB_COMMON_API_URL="http://127.0.0.1:18901/common"
export AB_TEST_SCHEDULER_INTERVAL_SEC="${SCHED:-20}"
export MEDIA_ROOT="${MEDIA_ROOT:-$RUN/media}"
export FRONTEND_URL="http://127.0.0.1:5173"
export CORS_ORIGINS="http://127.0.0.1:5173"
export NO_PROXY="127.0.0.1,localhost"
export no_proxy="127.0.0.1,localhost"
mkdir -p "$RUN"

case "${1:-status}" in
  up)
    command -v psql >/dev/null || { echo "psql is required" >&2; exit 2; }
    command -v createdb >/dev/null || { echo "createdb is required" >&2; exit 2; }
    "$PY" -c "import asyncpg" >/dev/null 2>&1 || { echo "asyncpg is required in the selected Python environment" >&2; exit 2; }
    if ! psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d postgres -Atqc "SELECT 1 FROM pg_database WHERE datname='${PGDATABASE}'" | grep -q 1; then
      createdb -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" "$PGDATABASE"
    fi
    command -v curl >/dev/null || { echo "curl is required" >&2; exit 2; }
    (cd "$ROOT" && PYTHONPATH=backend "$PY" -m alembic -c backend/alembic.ini upgrade head)
    if ! curl -sf http://127.0.0.1:18901/health >/dev/null 2>&1; then
      (cd "$HARNESS" && setsid nohup "$PY" -m uvicorn mock_wb:app --host 127.0.0.1 --port 18901 --log-level warning > "$RUN/mock.log" 2>&1 &)
    fi
    if ! curl -sf http://127.0.0.1:18900/health >/dev/null 2>&1; then
      (cd "$RUN" && setsid nohup env ABTEST_SRC="$ROOT" PYTHONPATH="$ROOT/backend" "$PY" "$HARNESS/run_backend.py" >> "$RUN/backend.log" 2>&1 &)
    fi
    for i in $(seq 1 60); do
      if curl -sf http://127.0.0.1:18900/health >/dev/null 2>&1 && curl -sf -X POST http://127.0.0.1:18901/_ctl/reset >/dev/null 2>&1; then
        echo "STACK UP"; exit 0
      fi
      sleep 1
    done
    echo "STACK NOT READY"; tail -50 "$RUN/backend.log" || true; tail -20 "$RUN/mock.log" || true; exit 1
    ;;
  down)
    pkill -f "$HARNESS/run_backend.py" || true
    pkill -f "uvicorn mock_wb:app" || true
    echo "STACK DOWN"
    ;;
  reset)
    rm -rf "$RUN/media"
    curl -sf -X POST http://127.0.0.1:18901/_ctl/reset >/dev/null
    echo "MOCK RESET"
    ;;
  status)
    echo "Backend:"; curl -s http://127.0.0.1:18900/health || true; echo
    echo "Mock:"; curl -s http://127.0.0.1:18901/_ctl/state || true; echo
    ;;
  *)
    echo "usage: $0 up|down|reset|status" >&2; exit 2 ;;
esac
