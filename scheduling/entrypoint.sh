#!/usr/bin/env bash
# Container entrypoint. Initialises the DB schema then dispatches a mode.
#   schedule HH:MM  -> long-lived in-process daily scheduler (default)
#   once            -> single daily run then exit
#   backtest        -> run the backtest once then exit
set -euo pipefail

cd /app
python scripts/init_db.py || true   # idempotent; never block startup

MODE="${1:-schedule}"
case "$MODE" in
  schedule)
    TIME="${2:-22:30}"
    echo "[entrypoint] in-process scheduler, daily at ${TIME} (TZ=${TZ:-UTC})"
    exec python scripts/run_daily.py --schedule "${TIME}"
    ;;
  once)
    echo "[entrypoint] single daily run"
    exec python scripts/run_daily.py
    ;;
  backtest)
    echo "[entrypoint] backtest"
    exec python scripts/run_backtest.py
    ;;
  *)
    echo "[entrypoint] passing through: $*"
    exec "$@"
    ;;
esac
