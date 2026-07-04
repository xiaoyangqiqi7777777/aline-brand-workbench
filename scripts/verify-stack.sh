#!/usr/bin/env sh

set -eu

GATEWAY_PORT="${GATEWAY_PORT:-8080}"
WEB_PORT="${WEB_PORT:-3000}"
API_PORT="${API_PORT:-8000}"
MINIO_CONSOLE_PORT="${MINIO_CONSOLE_PORT:-9001}"

check_url() {
  name="$1"
  url="$2"
  printf '%s\n' "[verify] $name: $url"
  curl --fail --silent --show-error --max-time 10 "$url" >/dev/null
}

docker compose ps
check_url "gateway" "http://127.0.0.1:${GATEWAY_PORT}/health"
check_url "web" "http://127.0.0.1:${WEB_PORT}/api/health"
check_url "api" "http://127.0.0.1:${API_PORT}/api/v1/health/ready"
check_url "api docs" "http://127.0.0.1:${API_PORT}/api/docs"
check_url "minio console" "http://127.0.0.1:${MINIO_CONSOLE_PORT}/"
printf '%s\n' "[verify] 所有服务验收通过。"
