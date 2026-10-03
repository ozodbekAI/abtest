#!/usr/bin/env bash
# Стенд: PostgreSQL 16 :55432, поддельный WB :18901, backend WB Optimizer :18900.
# stack_cloud.sh up | down | reset-db | psql "SQL" | status
set -u
H="$(cd "$(dirname "$0")" && pwd)"
export ABTEST_SRC="${ABTEST_SRC:-/tmp/claude-0/-home-claude/73b304ac-d90f-5d27-9dc9-48112979fca3/scratchpad/v4}"
PY="/tmp/claude-0/-home-claude/73b304ac-d90f-5d27-9dc9-48112979fca3/scratchpad/abtest/venv/bin/python"
RUN="${RUN:-/tmp/claude-0/-home-claude/73b304ac-d90f-5d27-9dc9-48112979fca3/scratchpad/e2e4}"
mkdir -p "$RUN"
PSQL="psql -h 127.0.0.1 -p 55432 -U postgres -v ON_ERROR_STOP=1 -q"

env_backend() {
  export APP_ENV=development
  export DATABASE_URL="postgresql+asyncpg://postgres@127.0.0.1:55432/wb_optimizer"
  export AUTO_CREATE_TABLES=false
  export JWT_SECRET_KEY="e2e-$(cat "$RUN/jwt")"
  export FERNET_KEY="$(cat "$RUN/fernet")"
  export MEDIA_SIGNING_SECRET="e2e-media-secret-long-enough-0123456789"
  export WB_CONTENT_API_URL=http://127.0.0.1:18901/c
  export WB_ADVERT_API_URL=http://127.0.0.1:18901/p
  export WB_ANALYTICS_API_URL=http://127.0.0.1:18901/a
  export WB_STATISTICS_API_URL=http://127.0.0.1:18901/s
  export WB_COMMON_API_URL=http://127.0.0.1:18901/common
  export AB_TEST_SCHEDULER_INTERVAL_SEC=${SCHED:-20}
  export MEDIA_ROOT="$RUN/media"
  export FRONTEND_URL=http://127.0.0.1:5173
  export CORS_ORIGINS=http://127.0.0.1:5173
  export NO_PROXY="127.0.0.1,localhost" no_proxy="127.0.0.1,localhost"
}

case "${1:-}" in
  up)
    [ -f "$RUN/fernet" ] || "$PY" -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())" > "$RUN/fernet"
    [ -f "$RUN/jwt" ] || head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n' > "$RUN/jwt"
    env_backend
    (cd "$ABTEST_SRC" && PYTHONPATH=backend "$PY" -m alembic -c backend/alembic.ini upgrade head > "$RUN/alembic.log" 2>&1) || { echo "ALEMBIC FAILED"; tail -20 "$RUN/alembic.log"; exit 1; }
    pgrep -f "uvicorn mock_wb:app" >/dev/null || (cd "$H" && setsid nohup "$PY" -m uvicorn mock_wb:app --host 127.0.0.1 --port 18901 --log-level warning > "$RUN/mock.log" 2>&1 &)
    pgrep -f "run_backend.py" >/dev/null || (cd "$RUN" && setsid nohup "$PY" "$H/run_backend.py" >> "$RUN/backend.log" 2>&1 &)
    for i in $(seq 1 40); do
      curl -sf --noproxy '*' http://127.0.0.1:18900/health >/dev/null 2>&1 && curl -sf --noproxy '*' -X POST http://127.0.0.1:18901/_ctl/reset >/dev/null 2>&1 && { echo "STACK UP"; exit 0; }
      sleep 1
    done
    echo "STACK NOT READY"; tail -30 "$RUN/backend.log"; tail -10 "$RUN/mock.log"; exit 1 ;;
  down)
    pkill -f run_backend.py; pkill -f "uvicorn mock_wb:app"; sleep 1; echo down ;;
  restart-backend)
    pkill -f run_backend.py; sleep 1; env_backend; (cd "$RUN" && setsid nohup "$PY" "$H/run_backend.py" >> "$RUN/backend.log" 2>&1 &)
    for i in $(seq 1 30); do curl -sf --noproxy '*' http://127.0.0.1:18900/health >/dev/null 2>&1 && { echo restarted; exit 0; }; sleep 1; done; echo "not ready" ;;
  reset-db)
    pkill -f run_backend.py; sleep 1
    $PSQL -c "drop database if exists wb_optimizer;" -c "create database wb_optimizer;"
    rm -rf "$RUN/media"; : > "$RUN/backend.log"; echo reset ;;
  psql)
    $PSQL -d wb_optimizer -c "$2" ;;
  ticker)
    env_backend; export AB_TEST_SCHEDULER_INTERVAL_SEC=36000 RUN="$RUN"
    pkill -f "[r]un_backend"; pkill -f "[t]icker.py"; sleep 1
    (cd "$RUN" && setsid nohup "$PY" "$H/run_backend.py" >> "$RUN/backend.log" 2>&1 &)
    (cd "$RUN" && setsid nohup "$PY" "$H/ticker.py" >> "$RUN/ticker.log" 2>&1 &)
    for i in $(seq 1 30); do curl -sf --noproxy '*' http://127.0.0.1:18900/health >/dev/null 2>&1 && { echo "backend+ticker up"; exit 0; }; sleep 1; done; echo "not ready" ;;
  status)
    pgrep -af "run_backend.py|mock_wb" ; curl -s --noproxy '*' http://127.0.0.1:18900/health; echo ;;
  *) echo "usage: $0 up|down|restart-backend|reset-db|psql SQL|status" ;;
esac
