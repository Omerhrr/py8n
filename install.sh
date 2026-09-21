#!/usr/bin/env bash
# Py8n one-command install (v120) - builds the frontend, starts the whole
# stack (postgres + redis + migrate + api + celery worker + llm-bridge +
# frontend + reverse proxy) and waits for the health door to answer.
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is required - https://docs.docker.com/get-docker/" >&2
  exit 1
fi
if docker compose version >/dev/null 2>&1; then COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then COMPOSE="docker-compose"
else echo "docker compose is required" >&2; exit 1; fi

echo "[py8n] building + starting the stack (first build takes a while)..."
$COMPOSE up -d --build

echo "[py8n] waiting for the API health door on :8025 ..."
for i in $(seq 1 120); do
  if curl -fsS http://localhost:8025/api/v1/health >/dev/null 2>&1; then
    echo "[py8n] UP - open http://localhost:8025"
    exit 0
  fi
  sleep 2
done
echo "[py8n] still warming up - follow with: $COMPOSE logs -f api" >&2
