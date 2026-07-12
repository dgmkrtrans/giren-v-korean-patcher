#!/usr/bin/env bash
set -euo pipefail

start_cron() {
  cron -f &
  CRON_PID=$!
  echo "[entrypoint] cron started pid=${CRON_PID}"
}

start_cron

(
  while true; do
    sleep 60
    if ! kill -0 "${CRON_PID}" 2>/dev/null; then
      echo "[entrypoint] cron stopped; restarting"
      start_cron
    fi
  done
) &

/usr/local/bin/backup-textures-manifest || true

exec "$@"
